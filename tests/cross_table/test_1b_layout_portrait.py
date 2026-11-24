# -*- coding: utf-8 -*-
"""1B layout disclose facts on real table (no live AI).

依赖私有目录「刁难抽查/」（已 .gitignore）。本机无该目录时 skip，不算产品回归。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.block_cross_table.query_engine.table_load import (
    build_minimal_portrait,
    load_flat_xlsx,
)

ROOT = Path(__file__).resolve().parents[2]


def _hard_dir() -> Path:
    for p in ROOT.iterdir():
        if p.is_dir() and "刁难" in p.name:
            return p
    pytest.skip("私有目录「刁难抽查/」不存在（已 gitignore），跳过真实表画像用例")


def test_1b_matrix_portrait_on_substation_contact():
    hard = _hard_dir()
    path = hard / "变电站对应负责人联系人.xlsx"
    if not path.is_file():
        pytest.skip("缺少私有表：变电站对应负责人联系人.xlsx")
    loaded = load_flat_xlsx(path, table_id="t_m", max_rows=40)
    port = build_minimal_portrait(loaded, structure_status="layers_recognized")
    assert port.get("layout") in ("ordered", "matrix", "unknown")
    assert port.get("layout") == "matrix"
    assert isinstance(port.get("merge_anchors"), list)
    assert len(port["merge_anchors"]) >= 1
    assert port.get("synthetic_column_count", 0) >= 1
    assert isinstance(port.get("header_rows_candidate"), int)


def test_1b_ordered_portrait_on_task_detail():
    hard = _hard_dir()
    path = hard / "已下发任务详情.xlsx"
    if not path.is_file():
        pytest.skip("缺少私有表：已下发任务详情.xlsx")
    loaded = load_flat_xlsx(path, table_id="t_o", max_rows=40)
    port = build_minimal_portrait(loaded, structure_status="layers_recognized")
    assert port.get("layout") == "ordered"
    assert port.get("synthetic_column_count", 0) == 0
    assert port.get("merge_anchors") == []
