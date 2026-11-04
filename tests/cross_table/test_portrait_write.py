# -*- coding: utf-8 -*-
"""A 段画像映射：8 字段映射表、无此结构枚举、写失败三码、重写门禁。"""
from __future__ import annotations

import pytest

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.portrait_write import (
    A_PRIME_TO_PORTRAIT_FIELD,
    FAIL_FIELD_MISSING,
    FAIL_MAPPING_UNDEFINED,
    FAIL_WRITE_REJECTED,
    InMemoryPortraitStore,
    PortraitWriteRetryGate,
    STRUCTURE_STATUS_LAYERS_RECOGNIZED,
    STRUCTURE_STATUS_UPSTREAM_ABSENT,
    assert_mapping_covers_required,
    b_should_read_layer_objects,
    mapping_table,
    read_required_layer_fields,
    rewrite_allowed,
    write_block_portrait,
)


def _ok_layer_result(**overrides):
    base = {
        "rect_to_layer": {"merge:0": "L0", "merge:1": "L1"},
        "layer_order": ["L0", "L1"],
        "layers": [{"layer_id": "L0", "rect_ids": ["merge:0"]}],
        "subtable_boundaries": [{"subtable_id": 0, "locator": {}}],
        "nesting_facts": [],
        "group_label_declaration": [],
        "dimension_membership": [
            {"dimension_field_id": "d0", "subtable_id": 0, "layer_ids": []}
        ],
        "header_completeness": "complete",
    }
    base.update(overrides)
    return base


def test_mapping_table_covers_all_eight_contract_fields():
    required = read_required_layer_fields()
    assert len(required) == 8
    assert_mapping_covers_required()
    table = mapping_table()
    for field in required:
        assert field in table
        assert table[field] == field  # 本轮 1:1
    assert set(table) == set(A_PRIME_TO_PORTRAIT_FIELD)
    assert set(required) == set(table)


def test_write_ok_package_maps_eight_fields_and_structure_recognized():
    store = InMemoryPortraitStore()
    layer = _ok_layer_result()
    out = write_block_portrait(
        {"status": "ok", "block_id": 0, "layer_result": layer},
        store,
    )
    assert out["ok"] is True
    assert out["failure_code"] is None
    slice_data = store.get(0)
    assert slice_data is not None
    assert slice_data["structure_status"] == STRUCTURE_STATUS_LAYERS_RECOGNIZED
    for a_field, p_field in A_PRIME_TO_PORTRAIT_FIELD.items():
        assert p_field in slice_data
        assert slice_data[p_field] == layer[a_field]
    assert b_should_read_layer_objects(slice_data) is True


def test_empty_dimension_membership_written_as_empty_list_not_missing():
    store = InMemoryPortraitStore()
    layer = _ok_layer_result(dimension_membership=[])
    out = write_block_portrait(
        {"status": "ok", "block_id": "b1", "layer_result": layer},
        store,
    )
    assert out["ok"] is True
    slice_data = store.get("b1")
    assert "dimension_membership" in slice_data
    assert slice_data["dimension_membership"] == []
    assert b_should_read_layer_objects(slice_data) is True


def test_no_structure_writes_enum_marker_not_empty_layers():
    store = InMemoryPortraitStore()
    out = write_block_portrait(
        {"status": "no_structure", "block_id": 7},
        store,
    )
    assert out["ok"] is True
    slice_data = store.get(7)
    assert slice_data["structure_status"] == STRUCTURE_STATUS_UPSTREAM_ABSENT
    assert "layers" not in slice_data
    assert "dimension_membership" not in slice_data
    assert "rect_to_layer" not in slice_data
    assert b_should_read_layer_objects(slice_data) is False


def test_field_missing_no_rewrite():
    store = InMemoryPortraitStore()
    layer = _ok_layer_result()
    del layer["header_completeness"]
    out = write_block_portrait(
        {"status": "ok", "block_id": 0, "layer_result": layer},
        store,
    )
    assert out["ok"] is False
    assert out["failure_code"] == FAIL_FIELD_MISSING
    assert out["rewrite_allowed"] is False
    assert rewrite_allowed(FAIL_FIELD_MISSING) is False
    assert out["error_class"] == "cross_table_contract_invalid"
    assert store.get(0) is None


def test_field_missing_none_treated_missing():
    store = InMemoryPortraitStore()
    layer = _ok_layer_result(rect_to_layer=None)
    out = write_block_portrait(
        {"status": "ok", "block_id": 0, "layer_result": layer},
        store,
    )
    assert out["failure_code"] == FAIL_FIELD_MISSING
    assert out["rewrite_allowed"] is False


def test_mapping_undefined_when_required_field_unmapped(monkeypatch):
    store = InMemoryPortraitStore()
    import pipeline.block_cross_table.portrait_write as pw

    monkeypatch.delitem(pw.A_PRIME_TO_PORTRAIT_FIELD, "header_completeness")
    layer = _ok_layer_result()
    out = write_block_portrait(
        {"status": "ok", "block_id": 0, "layer_result": layer},
        store,
    )
    assert out["ok"] is False
    assert out["failure_code"] == FAIL_MAPPING_UNDEFINED
    assert out["rewrite_allowed"] is False
    assert out["error_class"] == "cross_table_contract_invalid"
    assert store.get(0) is None


def test_mapping_undefined_when_portrait_key_empty(monkeypatch):
    store = InMemoryPortraitStore()
    import pipeline.block_cross_table.portrait_write as pw

    monkeypatch.setitem(pw.A_PRIME_TO_PORTRAIT_FIELD, "layers", "")
    out = write_block_portrait(
        {"status": "ok", "block_id": 0, "layer_result": _ok_layer_result()},
        store,
    )
    assert out["failure_code"] == FAIL_MAPPING_UNDEFINED
    assert out["rewrite_allowed"] is False
    assert store.get(0) is None


def test_write_rejected_rewrite_allowed_gate_respects_max_2():
    store = InMemoryPortraitStore()
    package = {
        "status": "ok",
        "block_id": 0,
        "layer_result": _ok_layer_result(),
    }
    store.reject_next = True
    first = write_block_portrait(package, store)
    assert first["failure_code"] == FAIL_WRITE_REJECTED
    assert first["rewrite_allowed"] is True
    assert first["error_class"] == "cross_table_input_invalid"
    assert store.get(0) is None

    gate = PortraitWriteRetryGate()
    assert gate.max_retries == 2

    # 第 1 次用户确认仍拒绝
    store.reject_next = True
    r1 = gate.user_confirm_rewrite(
        package, store, prior_failure_code=FAIL_WRITE_REJECTED
    )
    assert r1["ok"] is False
    assert r1["retries_used"] == 1
    assert r1["rewrite_allowed"] is True

    # 第 2 次用户确认成功
    store.reject_next = False
    r2 = gate.user_confirm_rewrite(
        package, store, prior_failure_code=FAIL_WRITE_REJECTED
    )
    assert r2["ok"] is True
    assert r2["retries_used"] == 2
    assert store.get(0) is not None

    # 已用满 2 次：再确认也不写
    store.slices.clear()
    store.reject_next = True
    # 新 gate 模拟达上限后再确认
    gate2 = PortraitWriteRetryGate()
    store.reject_next = True
    gate2.user_confirm_rewrite(
        package, store, prior_failure_code=FAIL_WRITE_REJECTED
    )
    store.reject_next = True
    gate2.user_confirm_rewrite(
        package, store, prior_failure_code=FAIL_WRITE_REJECTED
    )
    assert gate2.used == 2
    before = list(store.slices.keys())
    r3 = gate2.user_confirm_rewrite(
        package, store, prior_failure_code=FAIL_WRITE_REJECTED
    )
    assert r3["ok"] is False
    assert r3["retry_exhausted"] is True
    assert r3["rewrite_allowed"] is False
    assert list(store.slices.keys()) == before


def test_field_missing_user_confirm_forbidden():
    store = InMemoryPortraitStore()
    gate = PortraitWriteRetryGate()
    package = {
        "status": "ok",
        "block_id": 0,
        "layer_result": _ok_layer_result(),
    }
    out = gate.user_confirm_rewrite(
        package, store, prior_failure_code=FAIL_FIELD_MISSING
    )
    assert out["ok"] is False
    assert out["rewrite_allowed"] is False
    assert out["retries_used"] == 0
    assert store.get(0) is None


def test_mapping_undefined_user_confirm_forbidden():
    store = InMemoryPortraitStore()
    gate = PortraitWriteRetryGate()
    out = gate.user_confirm_rewrite(
        {"status": "ok", "block_id": 0, "layer_result": _ok_layer_result()},
        store,
        prior_failure_code=FAIL_MAPPING_UNDEFINED,
    )
    assert out["rewrite_allowed"] is False
    assert out["retries_used"] == 0


def test_reject_review_failed_package_not_for_a():
    store = InMemoryPortraitStore()
    with pytest.raises(CrossTableError) as ei:
        write_block_portrait(
            {"status": "review_failed", "block_id": 0},
            store,
        )
    assert ei.value.code == "cross_table_input_invalid"


def test_reject_ok_package_with_incomplete_header():
    store = InMemoryPortraitStore()
    with pytest.raises(CrossTableError) as ei:
        write_block_portrait(
            {
                "status": "ok",
                "block_id": 0,
                "layer_result": _ok_layer_result(header_completeness="incomplete"),
            },
            store,
        )
    assert ei.value.code == "cross_table_input_invalid"


def test_a_does_not_import_layer_recognize_run():
    """A 只消费包；模块不依赖 run_layer_recognize_block。"""
    import pipeline.block_cross_table.portrait_write as mod
    import inspect

    src = inspect.getsource(mod)
    assert "run_layer_recognize_block" not in src
    assert "write_trace" not in src
    assert "layer_recognize_trace" not in src
