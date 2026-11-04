# -*- coding: utf-8 -*-
"""I-layer slice bridge: capacity windows + plan gate (no live AI)."""
from __future__ import annotations

import pytest

from pipeline.block_cross_table.query_engine.errors import QueryEngineError
from pipeline.block_cross_table.query_engine.i_slice_bridge import (
    assert_plan_respects_slice_constraints,
    build_i_slice_route,
    merge_slice_qe_nrows,
    propose_row_slices,
)
from pipeline.block_cross_table.query_engine.ie_query import route_before_qp
from pipeline.block_cross_table.query_engine.table_load import load_flat_xlsx
from openpyxl import Workbook
from pathlib import Path


def test_propose_row_slices_50k_into_5():
    slices = propose_row_slices(50_000, rows_per_slice=10_000)
    assert len(slices) == 5
    assert slices[0]["row_range"] == [0, 10_000]
    assert slices[-1]["row_range"] == [40_000, 50_000]


def test_route_before_qp_strips_truncation_and_attaches_constraint():
    portraits = [
        {
            "table_id": "t1",
            "truncated": True,
            "original_rows": 50_000,
            "loaded_rows": 80,
            "truncation_fact": "truncated=True (loaded 80/50000)",
            "structure_status": "layers_recognized",
        }
    ]
    out = route_before_qp(portraits, active_index=0, rows_per_slice=10_000)
    assert out["needed"] is True
    assert out["route"] == "slice_then_qp"
    assert out["slice_constraints"]["t1"]["slices_n"] == 5
    p0 = out["portraits"][0]
    assert "truncated" not in p0
    assert "truncation_fact" not in p0
    assert p0["slice_constraint"]["row_range"] == [0, 10_000]


def test_assert_plan_respects_slice_constraints():
    cons = {"t1": {"row_range": [0, 10000], "active_slice_id": "s0"}}
    assert_plan_respects_slice_constraints(
        {"slice_constraint_ack": True, "steps": []}, cons
    )
    # I already windowed tables → ok without ack
    tables = {"t1": {"columns": ["a"], "rows": [{}] * 10000}}
    assert_plan_respects_slice_constraints({"steps": []}, cons, tables_by_id=tables)
    with pytest.raises(QueryEngineError) as ei:
        assert_plan_respects_slice_constraints({"steps": []}, cons)
    assert getattr(ei.value, "subreason", None) == "slice_constraint_ignored"


def test_load_window_mode_no_silent_truncate(tmp_path: Path):
    path = tmp_path / "t.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["a"])
    for i in range(20):
        ws.append([i])
    wb.save(path)
    wb.close()
    win = load_flat_xlsx(
        path, table_id="t", max_rows=5, row_offset=5, window_mode=True
    )
    assert win["truncated"] is False
    assert win["loaded_rows"] == 5
    assert [r["a"] for r in win["rows"]] == [5, 6, 7, 8, 9]


def test_cross_slice_bind_when_two_large_tables():
    portraits = [
        {"table_id": "a", "truncated": True, "original_rows": 20_000},
        {"table_id": "b", "truncated": True, "original_rows": 20_000},
    ]
    r = build_i_slice_route(portraits, rows_per_slice=10_000)
    assert r["cross_slice_bind_required"] is True
    assert r["route"] == "conflict_or_needs_user"


def test_i_slice_bridge_rejects_plan_out_of_window():
    cons = {"t1": {"row_range": [0, 10000], "active_slice_id": "s0"}}
    tables = {"t1": {"columns": ["a"], "rows": [{}] * 10000}}
    plan = {
        "steps": [
            {
                "step_id": "s1",
                "op": "op_filter",
                "args": {"row_range": [0, 20000]},
            }
        ]
    }
    with pytest.raises(QueryEngineError) as ei:
        assert_plan_respects_slice_constraints(plan, cons, tables_by_id=tables)
    assert getattr(ei.value, "subreason", None) == "slice_constraint_out_of_window"


def test_slice_merge_total_rows_equals_original():
    assert merge_slice_qe_nrows([10000, 10000, 10000, 10000, 10000]) == 50000
    assert merge_slice_qe_nrows([10000, None, 10000]) == 20000
