# -*- coding: utf-8 -*-
"""v1.7.0 A' trace 审计落盘：必补 3/4/5 + 细节 A/B/C（T1–T11）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.join_hypothesis import reset_trace_seq, write_trace
from pipeline.block_cross_table.layer_recognize import (
    project_upstream_rectangles,
    run_layer_recognize_block,
)
from pipeline.block_cross_table.layer_recognize_trace import (
    KIND_ASK_SUBSET,
    KIND_FORMAL_REVIEW,
    KIND_PROPOSE,
    LAYER_RECOGNIZE_TRACE_BODY_REQUIRED,
    TRACE_TRUNCATE_MARKER,
    list_traces_for_block,
    load_trace_file,
    reset_block_id_registry,
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


def _body_of(item: dict) -> dict:
    data = load_trace_file(item["path"])
    return data


def _assert_body_11(body: dict) -> None:
    for key in LAYER_RECOGNIZE_TRACE_BODY_REQUIRED:
        assert key in body, "missing body field %s" % key


def test_t1_propose_ok_trace_11_fields_empty_reasons(tmp_path, monkeypatch):
    """T1 正常成功：propose→validate 通过→落 trace，11 字段，validate_reasons=[]。"""
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return _good_proposal()
        return {"passed": True, "reason": "ok"}

    run_id = "audit-t1"
    out = run_layer_recognize_block(
        _block(),
        _draft(),
        _tables(),
        {},
        "识别中文",
        100_000,
        chat=chat,
        run_id=run_id,
        entry_seq=1,
        clock=lambda: "T1",
    )
    assert out["status"] == "ok"
    items = [
        x
        for x in list_traces_for_block(run_id, 0, trace_dir=trace_dir)
        if x["meta"]["call_kind"] == KIND_PROPOSE
    ]
    assert len(items) == 1
    body = _body_of(items[0])
    _assert_body_11(body)
    assert body["validate_reasons"] == []
    assert body["raw_truncated"] is False
    assert body["prompt_truncated"] is False
    assert isinstance(body["raw_full"], dict)
    # ensure_ascii=False：中文 prompt 时 bytes > chars
    assert body["prompt_bytes"] >= body["prompt_chars"]
    text_on_disk = Path(items[0]["path"]).read_text(encoding="utf-8")
    raw_seg = json.dumps(body["raw_full"], ensure_ascii=False, default=str)
    assert len(raw_seg) == body["raw_chars"]
    assert "识别中文" in text_on_disk or body["prompt_chars"] > 0


def test_t2_validate_false_reasons(tmp_path, monkeypatch):
    """T2 validate ok=False 有 reasons：validate_reasons == validate.reasons。"""
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 2
    )

    def chat(prompt: str):
        return {"answer": "x"}

    run_id = "audit-t2"
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
        clock=lambda: "T2",
    )
    assert out["status"] == "conflict"
    items = [
        x
        for x in list_traces_for_block(run_id, 0, trace_dir=trace_dir)
        if x["meta"]["call_kind"] == KIND_PROPOSE
    ]
    assert len(items) >= 1
    body = _body_of(items[0])
    # {"answer": ...} is an illicit envelope (policy A: detect, never unwrap).
    assert body["validate_reasons"] == ["envelope_wrapper_forbidden"]


def test_t3_validate_raises(tmp_path, monkeypatch):
    """T3 validate 抛异常：validate_reasons == validate_exception:<Type>。"""
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )

    def boom(*_a, **_k):
        raise RuntimeError("validate_boom")

    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize.validate_layer_proposal",
        boom,
    )

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return _good_proposal()
        return {"passed": True, "reason": "ok"}

    run_id = "audit-t3"
    with pytest.raises(RuntimeError, match="validate_boom"):
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
            clock=lambda: "T3",
        )
    items = [
        x
        for x in list_traces_for_block(run_id, 0, trace_dir=trace_dir)
        if x["meta"]["call_kind"] == KIND_PROPOSE
    ]
    assert len(items) == 1
    assert _body_of(items[0])["validate_reasons"] == [
        "validate_exception:RuntimeError"
    ]


def test_t4_propose_non_dict_raw_received(tmp_path, monkeypatch):
    """T4 propose 返回非 dict：raw_full 可见真实返回，raw_received 分支。"""
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )

    def chat(prompt: str):
        return "hello"

    run_id = "audit-t4"
    with pytest.raises(CrossTableError) as ctx:
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
            clock=lambda: "T4",
        )
    assert "not object" in ctx.value.message
    items = [
        x
        for x in list_traces_for_block(run_id, 0, trace_dir=trace_dir)
        if x["meta"]["call_kind"] == KIND_PROPOSE
    ]
    assert len(items) == 1
    body = _body_of(items[0])
    assert body["raw_full"] == "hello"
    assert body["validate_reasons"] == [
        "propose_exception:CrossTableError"
    ]


def test_t5_model_call_raises_no_raw_received(tmp_path, monkeypatch):
    """T5 模型调用本身抛异常：raw_full 为 propose_exception 串。"""
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )

    def chat(prompt: str):
        raise ConnectionError("llm_down")

    run_id = "audit-t5"
    with pytest.raises(ConnectionError, match="llm_down"):
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
            clock=lambda: "T5",
        )
    items = [
        x
        for x in list_traces_for_block(run_id, 0, trace_dir=trace_dir)
        if x["meta"]["call_kind"] == KIND_PROPOSE
    ]
    assert len(items) == 1
    body = _body_of(items[0])
    assert isinstance(body["raw_full"], str)
    assert body["raw_full"].startswith("propose_exception:ConnectionError:")
    assert "llm_down" in body["raw_full"]
    assert body["validate_reasons"] == [
        "propose_exception:ConnectionError"
    ]


def test_t6_ask_subset_exception(tmp_path, monkeypatch):
    """T6 ask_subset 抛异常：validate_reasons == ask_subset_exception:<Type>。"""
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
    calls = {"n": 0}

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return _good_proposal()
        calls["n"] += 1
        raise TimeoutError("subset_timeout")

    run_id = "audit-t6"
    with pytest.raises(TimeoutError, match="subset_timeout"):
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
            clock=lambda: "T6",
        )
    items = [
        x
        for x in list_traces_for_block(run_id, 0, trace_dir=trace_dir)
        if x["meta"]["call_kind"] == KIND_ASK_SUBSET
    ]
    assert len(items) == 1
    assert _body_of(items[0])["validate_reasons"] == [
        "ask_subset_exception:TimeoutError"
    ]


def test_t7_formal_review_exception(tmp_path, monkeypatch):
    """T7 formal_review 抛异常：validate_reasons == review_exception:<Type>。"""
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return _good_proposal()
        raise ValueError("review_boom")

    run_id = "audit-t7"
    with pytest.raises(ValueError, match="review_boom"):
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
            clock=lambda: "T7",
        )
    items = [
        x
        for x in list_traces_for_block(run_id, 0, trace_dir=trace_dir)
        if x["meta"]["call_kind"] == KIND_FORMAL_REVIEW
    ]
    assert len(items) == 1
    assert _body_of(items[0])["validate_reasons"] == [
        "review_exception:ValueError"
    ]


def test_t8_raw_over_capacity_truncation(tmp_path, monkeypatch):
    """T8 raw 超容量：raw_chars 原长，truncated=true，len(raw_full)==MAX。"""
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    max_chars = 64
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize_trace."
        "read_layer_recognize_trace_raw_max_chars",
        lambda contract_data=None: max_chars,
    )
    huge = {"answer": "X" * 200}

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return huge
        return {"passed": True, "reason": "ok"}

    # validate will fail (no rectangle_layer_map) → conflict, but trace still written
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 2
    )
    run_id = "audit-t8"
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
        clock=lambda: "T8",
    )
    items = [
        x
        for x in list_traces_for_block(run_id, 0, trace_dir=trace_dir)
        if x["meta"]["call_kind"] == KIND_PROPOSE
    ]
    assert len(items) >= 1
    body = _body_of(items[0])
    full_s = json.dumps(huge, ensure_ascii=False)
    assert body["raw_chars"] == len(full_s)
    assert body["raw_truncated"] is True
    assert isinstance(body["raw_full"], str)
    assert len(body["raw_full"]) == max_chars
    assert body["raw_full"].endswith(TRACE_TRUNCATE_MARKER.rstrip("\n")) or (
        TRACE_TRUNCATE_MARKER in body["raw_full"]
        or body["raw_full"].endswith("*/")
    )


def test_t9_write_trace_fail_keeps_business_primary(tmp_path, monkeypatch):
    """T9 write_trace 失败：主异常为 business，__cause__ 为 trace 侧。"""
    _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )

    def chat(prompt: str):
        return "hello"

    def boom_write(kind, body, clock):
        raise CrossTableError("trace_io_failed", "disk_full")

    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize_trace.write_trace",
        boom_write,
    )
    with pytest.raises(CrossTableError) as ctx:
        run_layer_recognize_block(
            _block(),
            _draft(),
            _tables(),
            {},
            "u",
            100_000,
            chat=chat,
            run_id="audit-t9",
            entry_seq=1,
            clock=lambda: "T9",
        )
    # 主异常是业务（layer proposal not object），cause 是 trace_io_failed
    assert ctx.value.code == "cross_table_proposal_invalid"
    assert "not object" in ctx.value.message
    cause = ctx.value.__cause__
    assert cause is not None
    assert isinstance(cause, CrossTableError)
    assert cause.code == "trace_io_failed"


def test_t10_review_two_calls_independent_prompt_out(tmp_path, monkeypatch):
    """T10 ask_subset 与 formal_review 独立 prompt_out：prompt_hash 不同。"""
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
    calls = {"review": 0}

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return _good_proposal()
        calls["review"] += 1
        if calls["review"] == 1:
            return {"review_locator_ids": ["merge:0"]}
        return {"passed": True, "reason": "ok"}

    run_id = "audit-t10"
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
        clock=lambda: "T10",
    )
    assert out["status"] == "ok"
    found = list_traces_for_block(run_id, 0, trace_dir=trace_dir)
    sub = [x for x in found if x["meta"]["call_kind"] == KIND_ASK_SUBSET]
    rev = [x for x in found if x["meta"]["call_kind"] == KIND_FORMAL_REVIEW]
    assert len(sub) == 1 and len(rev) == 1
    h_sub = _body_of(sub[0])["prompt_hash"]
    h_rev = _body_of(rev[0])["prompt_hash"]
    assert h_sub != h_rev
    assert len(h_sub) == 16 and len(h_rev) == 16


def test_t11_validate_false_empty_reasons_fallback(tmp_path, monkeypatch):
    """T11 ok=False 且 reasons=[] → validate_false_with_empty_reasons。"""
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 2
    )

    def fake_validate(*_a, **_k):
        return {"ok": False, "reasons": []}

    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize.validate_layer_proposal",
        fake_validate,
    )

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return _good_proposal()
        return {"passed": True, "reason": "ok"}

    run_id = "audit-t11"
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
        clock=lambda: "T11",
    )
    items = [
        x
        for x in list_traces_for_block(run_id, 0, trace_dir=trace_dir)
        if x["meta"]["call_kind"] == KIND_PROPOSE
    ]
    assert len(items) >= 1
    assert _body_of(items[0])["validate_reasons"] == [
        "validate_false_with_empty_reasons"
    ]


def test_write_trace_ensure_ascii_false_chinese_raw_chars(tmp_path, monkeypatch):
    """write_trace 序列化 ensure_ascii=False：中文 raw 落盘字符数 == raw_chars。"""
    _patch_trace_dir(monkeypatch, tmp_path)
    body = {
        "raw_full": {"答案": "中文内容"},
        "raw_chars": len(
            json.dumps({"答案": "中文内容"}, ensure_ascii=False)
        ),
    }
    path = write_trace("layer_propose", body, lambda: "ASCII0")
    text = Path(path)
    if not text.is_absolute():
        text = Path(__file__).resolve().parents[2] / path
    on_disk = text.read_text(encoding="utf-8")
    assert "中文内容" in on_disk
    assert "\\u" not in on_disk.split("中文内容")[0][-20:]
    loaded = json.loads(on_disk)
    assert loaded["raw_chars"] == len(
        json.dumps(loaded["raw_full"], ensure_ascii=False)
    )
