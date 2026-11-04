# -*- coding: utf-8 -*-
"""B2: snapshot_r2_keys."""

from __future__ import annotations

import pytest

from pipeline.block_cross_table.api import snapshot_r2_keys
from pipeline.block_cross_table.errors import CrossTableError


def _plan(block_id, parents):
    return {"blocks": [{"block_id": block_id, "parent_table_ids": parents}]}


def test_field_id_pair():
    exe = [
        {
            "sources_resolved": [
                {"block_id": 0, "field_id": "f0", "col": 0}
            ]
        }
    ]
    assert snapshot_r2_keys(exe, _plan(0, ["t0"])) == [["t0", "field:f0"]]


def test_col_pair():
    exe = [{"sources_resolved": [{"block_id": 0, "col": 0}]}]
    assert snapshot_r2_keys(exe, _plan(0, ["t0"])) == [["t0", "col:0"]]


def test_mixed_sorted():
    exe = [
        {
            "sources_resolved": [
                {"block_id": 0, "col": 2},
                {"block_id": 0, "field_id": "f0", "col": 0},
            ]
        }
    ]
    assert snapshot_r2_keys(exe, _plan(0, ["t0"])) == [
        ["t0", "col:2"],
        ["t0", "field:f0"],
    ]


def test_two_tables():
    exe = [
        {"sources_resolved": [{"block_id": 1, "field_id": "f1", "col": 0}]},
        {"sources_resolved": [{"block_id": 0, "field_id": "f0", "col": 0}]},
    ]
    plan = {
        "blocks": [
            {"block_id": 0, "parent_table_ids": ["t0"]},
            {"block_id": 1, "parent_table_ids": ["t1"]},
        ]
    }
    assert snapshot_r2_keys(exe, plan) == [
        ["t0", "field:f0"],
        ["t1", "field:f1"],
    ]


def test_missing_block():
    exe = [{"sources_resolved": [{"block_id": 9, "col": 0}]}]
    with pytest.raises(CrossTableError) as ei:
        snapshot_r2_keys(exe, _plan(0, ["t0"]))
    assert ei.value.code == "cross_table_input_invalid"


def test_parent_length_not_one():
    exe = [{"sources_resolved": [{"block_id": 0, "col": 0}]}]
    with pytest.raises(CrossTableError) as ei:
        snapshot_r2_keys(exe, _plan(0, ["t0", "t1"]))
    assert ei.value.code == "cross_table_input_invalid"


def test_empty_executable():
    assert snapshot_r2_keys([], _plan(0, ["t0"])) == []
