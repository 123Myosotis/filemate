# -*- coding: utf-8 -*-
"""QE sort must not TypeError on None cells."""
from __future__ import annotations

from pipeline.block_cross_table.query_engine.qe import _exec_sort_truncate


def test_qe_sort_handles_none_values():
    inp = {
        "columns": ["name", "score"],
        "rows": [
            {"name": "b", "score": 2},
            {"name": "n1", "score": None},
            {"name": "a", "score": 1},
            {"name": "n2", "score": None},
        ],
    }
    out = _exec_sort_truncate(inp, {"sort_column_id": "score", "direction": "asc"})
    scores = [r.get("score") for r in out["rows"]]
    assert scores == [1, 2, None, None]
    out_desc = _exec_sort_truncate(
        inp, {"sort_column_id": "score", "direction": "desc", "limit": 3}
    )
    assert [r.get("score") for r in out_desc["rows"]] == [None, None, 2]
