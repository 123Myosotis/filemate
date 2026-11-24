# -*- coding: utf-8 -*-
"""QE sort_truncate：规范化 sort/order_by；无有效列 fail closed。"""
from __future__ import annotations

import pytest

from pipeline.block_cross_table.query_engine.errors import QueryEngineError
from pipeline.block_cross_table.query_engine.qe import (
    _exec_sort_truncate,
    _normalize_sort_args,
)


def test_normalize_sort_string():
    out = _normalize_sort_args({"sort": "amount desc"})
    assert out["sort_column_id"] == "amount"
    assert out["direction"] == "desc"


def test_normalize_order_by_list():
    out = _normalize_sort_args({"order_by": [{"column": "ts", "direction": "asc"}]})
    assert out["sort_column_id"] == "ts"
    assert out["direction"] == "asc"


def test_sort_truncate_fail_closed_missing_col():
    inp = {"columns": ["a", "b"], "rows": [{"a": 1, "b": 2}]}
    with pytest.raises(QueryEngineError) as ctx:
        _exec_sort_truncate(inp, {"sort": "missing_col"})
    assert ctx.value.subreason == "sort_column_missing"


def test_sort_truncate_ok_via_order_by():
    inp = {
        "columns": ["n"],
        "rows": [{"n": 2}, {"n": 1}],
    }
    out = _exec_sort_truncate(inp, {"order_by": "n asc"})
    assert [r["n"] for r in out["rows"]] == [1, 2]


def test_normalize_sort_keys_alias():
    """模型偶发 sort_keys；只解析列+方向，不猜业务列。"""
    out = _normalize_sort_args({"sort_keys": [{"column": "金额", "direction": "desc"}]})
    assert out["sort_column_id"] == "金额"
    assert out["direction"] == "desc"
