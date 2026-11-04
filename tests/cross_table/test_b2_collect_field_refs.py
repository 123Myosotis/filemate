# -*- coding: utf-8 -*-
"""B2: collect_cross_table_field_refs."""

from __future__ import annotations

import pytest

from pipeline.block_cross_table.api import collect_cross_table_field_refs
from pipeline.block_cross_table.errors import CrossTableError


def _plan(*pairs):
    return {
        "blocks": [
            {"block_id": bid, "parent_table_ids": [tid]} for bid, tid in pairs
        ]
    }


def _ordered(target, block_id, cells):
    return {
        "target_name": target,
        "sources_resolved": [
            {
                "block_id": block_id,
                "row": row,
                "col": col,
                "cell_text": text,
                "kind": "column",
            }
            for row, col, text in cells
        ],
    }


def test_empty_executable():
    assert collect_cross_table_field_refs([], {}, {}, _plan((0, "t0"))) == []


def test_executable_not_list():
    with pytest.raises(CrossTableError) as ei:
        collect_cross_table_field_refs(None, {}, {}, _plan((0, "t0")))  # type: ignore[arg-type]
    assert ei.value.code == "cross_table_input_invalid"


def test_one_ordered_item():
    refs = collect_cross_table_field_refs(
        [_ordered("A", 0, [(1, 0, "A")])],
        {},
        {},
        _plan((0, "t0")),
    )
    assert len(refs) == 1
    assert refs[0]["role"] == "column"
    assert refs[0]["subtable_id"] is None
    assert refs[0]["parent_table_id"] == "t0"
    assert refs[0]["field_id"] == "col:t0:0"


def test_one_matrix_item():
    item = {
        "target_name": "A",
        "sources_resolved": [
            {
                "block_id": 0,
                "field_id": "f0",
                "row": 1,
                "col": 0,
                "cell_text": "A",
            }
        ],
    }
    draft = {
        0: {
            "fields": [
                {
                    "field_id": "f0",
                    "role": "group",
                    "subtable_id": 0,
                    "segments": [{"col_range": [0, 1]}],
                    "value_samples": ["x"],
                }
            ]
        }
    }
    refs = collect_cross_table_field_refs(
        [item], draft, {0: {0: ["f0"]}}, _plan((0, "t0"))
    )
    assert len(refs) == 1
    assert refs[0]["role"] == "group"
    assert refs[0]["in_dimensions"] is True
    assert refs[0]["subtable_id"] == 0
    assert "segments" not in refs[0]


def test_parent_table_id_per_block():
    items = [
        _ordered("A", 0, [(0, 0, "A")]),
        _ordered("B", 1, [(0, 0, "B")]),
    ]
    refs = collect_cross_table_field_refs(
        items, {}, {}, _plan((0, "t0"), (1, "t1"))
    )
    assert [r["parent_table_id"] for r in refs] == ["t0", "t1"]


def test_ordered_many_labels_empty_source_label():
    item = _ordered("A", 0, [(2, 0, "b"), (1, 1, "a"), (1, 0, "a")])
    refs = collect_cross_table_field_refs([item], {}, {}, _plan((0, "t0")))
    assert refs[0]["source_labels"] == ["a", "b"]
    assert refs[0]["source_label"] == ""


def test_ordered_one_label():
    item = _ordered("A", 0, [(1, 0, "  x  ")])
    refs = collect_cross_table_field_refs([item], {}, {}, _plan((0, "t0")))
    assert refs[0]["source_labels"] == ["x"]
    assert refs[0]["source_label"] == "x"


def test_in_dimensions_true():
    item = {
        "target_name": "A",
        "sources_resolved": [
            {"block_id": 0, "field_id": "f0", "row": 0, "col": 0, "cell_text": "A"}
        ],
    }
    draft = {0: {"fields": [{"field_id": "f0", "role": "group", "subtable_id": 0}]}}
    refs = collect_cross_table_field_refs(
        [item], draft, {0: {0: ["f0"]}}, _plan((0, "t0"))
    )
    assert refs[0]["in_dimensions"] is True


def test_value_samples_not_truncated():
    samples = ["s0", "s1", "s2", "s3", "s4"]
    item = {
        "target_name": "A",
        "sources_resolved": [
            {"block_id": 0, "field_id": "f0", "row": 0, "col": 0, "cell_text": "A"}
        ],
    }
    draft = {
        0: {
            "fields": [
                {
                    "field_id": "f0",
                    "role": "group",
                    "subtable_id": 0,
                    "value_samples": samples,
                }
            ]
        }
    }
    refs = collect_cross_table_field_refs([item], draft, {}, _plan((0, "t0")))
    assert refs[0]["value_samples"] == samples
    assert refs[0]["value_samples"] is not samples


def test_rejected_not_in_executable_is_absent():
    accepted = _ordered("A", 0, [(0, 0, "A")])
    rejected = [{"reason": "locator_not_on_grid", "target_name": "Z"}]
    refs = collect_cross_table_field_refs([accepted], {}, {}, _plan((0, "t0")))
    assert len(refs) == 1
    assert rejected[0]["target_name"] not in [r["target_name"] for r in refs]
